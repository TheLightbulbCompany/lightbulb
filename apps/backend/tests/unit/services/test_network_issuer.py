"""The Lightbulb Connect issuer: compact EdDSA proofs the engine verifies offline
(lightbulb-connect `src/proof.rs`), each mint audited."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import base64
import json

import boto3
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.config import settings
from core.services import network_issuer
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

SEED = bytes(range(32))
PUBLIC = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
NOW = 1_800_000_000


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def member_key(private: Ed25519PrivateKey) -> str:
    return base64.b32encode(private.public_key().public_bytes_raw()).decode().lower().rstrip("=")


def open_proof(token: str, public=PUBLIC) -> tuple[dict, dict]:
    """Verify like proof.rs: the signature is over the ASCII `header.payload`."""
    h, p, s = token.split(".")
    public.verify(b64d(s), f"{h}.{p}".encode())
    return json.loads(b64d(h)), json.loads(b64d(p))


@pytest.fixture
def issuer(monkeypatch):
    """The seed comes through the same seam production reads Secrets Manager through."""
    reads = []

    def read_seed():
        reads.append(settings.NETWORK_ISSUER_KEY_SECRET)
        return base64.b64encode(SEED).decode()

    monkeypatch.setattr(network_issuer, "_read_seed", read_seed)
    monkeypatch.setattr(network_issuer, "_key", None)
    monkeypatch.setattr(network_issuer, "ISSUER_PUBLIC_KEYS", {"dev": PUBLIC.public_bytes_raw()})
    monkeypatch.setattr(settings, "NETWORK_ISSUER_KEY_SECRET", "lightbulb/dev/network/issuer_key_dev")
    monkeypatch.setattr(settings, "NETWORK_ISSUER_KID", "dev")
    monkeypatch.setattr(settings, "NETWORK_ISSUER_URL", "https://dev.example/connect")
    return reads


@pytest.fixture
def audit(table, monkeypatch):
    t = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="test-network-audit",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(settings, "NETWORK_AUDIT_TABLE", "test-network-audit")
    return t


KEY = member_key(Ed25519PrivateKey.generate())


@pytest.mark.asyncio
async def test_proof_verifies_with_the_public_half_and_lasts_24h(issuer, audit):
    token = await network_issuer.mint("user_ada", "Ada", KEY, "computer", None, None, NOW)
    header, claims = open_proof(token)
    assert header == {"alg": "EdDSA", "kid": "dev"}
    assert claims["exp"] - claims["iat"] == 86_400 and claims["iat"] == NOW
    assert claims["iss"] == "https://dev.example/connect" and claims["kid"] == "dev"
    assert (claims["sub"], claims["name"], claims["computer"], claims["kind"]) == ("user_ada", "Ada", KEY, "computer")
    # proof.rs refuses `agents: null` and needs no workspace key for a free user.
    assert "workspace" not in claims and "agents" not in claims
    assert len(token) <= 4096
    # Read once, then cached for 5 minutes.
    await network_issuer.mint("user_ada", "Ada", KEY, "computer", None, None, NOW)
    assert issuer == ["lightbulb/dev/network/issuer_key_dev"]


@pytest.mark.asyncio
async def test_workspace_proof_sub_is_ws_of_the_owner(issuer, audit):
    ws = {"id": "org_a", "name": "The Lightbulb Company"}
    agents = ["Friday", "Engineering", "Finance", "Growth", "Analytics", "Customer Operations", "Extra"]
    token = await network_issuer.mint("ws:org_a", "The Lightbulb Company", KEY, "workspace", ws, agents, NOW)
    _, claims = open_proof(token)
    assert claims["sub"] == "ws:org_a" and claims["workspace"] == ws
    assert claims["agents"] == agents[:6]  # proof.rs: at most 6, each 1..=32 characters
    with pytest.raises(ValueError):
        await network_issuer.mint("ws:org_b", "X", KEY, "workspace", ws, ["Friday"], NOW)
    with pytest.raises(ValueError):
        await network_issuer.mint("ws:org_a", "Ada", KEY, "computer", None, None, NOW)  # a person is never ws:


@pytest.mark.asyncio
async def test_every_mint_writes_an_audit_row(issuer, audit):
    await network_issuer.mint("user_ada", "Ada", KEY, "computer", None, None, NOW)
    await network_issuer.mint("ws:org_a", "Acme", KEY, "workspace", {"id": "org_a", "name": "Acme"}, [], NOW + 1)
    rows = sorted(audit.scan()["Items"], key=lambda r: r["sk"])
    assert [(r["pk"], r["kind"], r["sub"], r["proof_kind"]) for r in rows] == [
        ("ISSUER", "proof", "user_ada", "computer"),
        ("ISSUER", "proof", "ws:org_a", "workspace"),
    ]
    assert rows[0]["sk"].startswith(f"{NOW}#proof#") and rows[0]["computer"] == KEY
    assert rows[0]["exp"] == NOW + 86_400 and rows[0]["expiresAt"] == NOW + 86_400 + 30 * 86_400
    assert not any(str(v).startswith("eyJ") for r in rows for v in r.values())  # no proof in the audit


def test_canonical_matches_the_engine():
    # identity.rs `canonical`: keys sorted recursively, no whitespace, UTF-8 left unescaped.
    assert network_issuer.canonical({"b": 1, "a": {"d": 2, "c": 3}}) == b'{"a":{"c":3,"d":2},"b":1}'
    assert network_issuer.canonical({"n": [1, 'é"']}) == '{"n":[1,"é\\""]}'.encode()


def test_member_key_is_52_lowercase_base32():
    assert network_issuer.member_key_ok(KEY)
    assert not network_issuer.member_key_ok(KEY.upper())
    assert not network_issuer.member_key_ok(KEY[:-1])
    assert not network_issuer.member_key_ok("a" * 51 + "1")


@pytest.mark.asyncio
async def test_a_seed_that_does_not_match_the_public_table_refuses_to_mint(issuer, audit, monkeypatch, caplog):
    from fastapi import HTTPException

    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    monkeypatch.setattr(network_issuer, "ISSUER_PUBLIC_KEYS", {"dev": other})  # stale after a ceremony
    with pytest.raises(HTTPException) as refused:
        await network_issuer.mint("user_ada", "Ada", KEY, "computer", None, None, NOW)
    assert refused.value.status_code == 503
    assert "does not match ISSUER_PUBLIC_KEYS[dev]" in caplog.text
    assert base64.b64encode(SEED).decode() not in caplog.text and SEED.hex() not in caplog.text
    assert audit.scan()["Items"] == []  # nothing minted
