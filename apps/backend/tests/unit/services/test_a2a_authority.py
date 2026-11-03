import pytest

from core.services import a2a_authority as a

REQ = a.AuthorityRequest(
    operation=a.Operation(skill="message", resource="agent:friday", operands={"text": "hi"}), intent="say hi"
)
CHAIN = [{"agent": None, "session": None, "person": "user_1", "at": 1}]


def _mint(monkeypatch, **over):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    kw = dict(requester_person="user_1", org="org_1", verified_by="clerk-session", chain=CHAIN, request=REQ, now=1_000)
    return a.mint(**{**kw, **over})


def test_operands_hash_is_order_independent():
    assert a.operands_hash({"b": 1, "a": 2}) == a.operands_hash({"a": 2, "b": 1})
    assert a.operands_hash({"a": 1}).startswith("sha256:")


def test_mint_then_verify_roundtrip(monkeypatch):
    jws, claims = _mint(monkeypatch)
    out = a.verify(jws, now=1_001)
    assert out["id"] == claims["id"] and out["exp"] == 1_000 + a.MAX_LIFE_S
    assert out["operation"]["operands_hash"] == a.operands_hash({"text": "hi"})
    assert out["chain"][0]["person"] == "user_1" and out["policy"] is None


def test_verify_rejects_expired_and_tampered(monkeypatch):
    jws, _ = _mint(monkeypatch)
    with pytest.raises(a.AuthorityError, match="exp"):
        a.verify(jws, now=1_000 + a.MAX_LIFE_S + 1)
    with pytest.raises(a.AuthorityError, match="iat"):
        a.verify(jws, now=999)
    head, body, sig = jws.split(".")
    with pytest.raises(a.AuthorityError, match="signature"):
        a.verify(
            f"{head}.{body}.{'B' if sig[0] != 'B' else 'C'}{sig[1:]}", now=1_001
        )  # flip the first byte; the last byte is often already zero


def test_verify_rejects_wrong_typ_and_long_intent(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    from core.services import network_proof

    proof = network_proof.mint("computer-key", "user_1", now=1_000)  # a sign-in proof, wrong typ
    with pytest.raises(a.AuthorityError, match="typ"):
        a.verify(proof, now=1_001)
    long = a.AuthorityRequest(operation=REQ.operation, intent="x" * (a.MAX_INTENT + 1))
    with pytest.raises(a.AuthorityError, match="intent_length"):
        _mint(monkeypatch, request=long, chain=[])


def test_verify_rejects_origin_and_operands_mismatch(monkeypatch):
    bad_chain = [{**CHAIN[0], "person": "user_2"}]
    jws, _ = _mint(monkeypatch, chain=bad_chain)
    with pytest.raises(a.AuthorityError, match="origin"):
        a.verify(jws, now=1_001)
    jws, _ = _mint(monkeypatch, request=a.AuthorityRequest(operation=REQ.operation, intent="x", on_behalf_of="user_2"))
    with pytest.raises(a.AuthorityError, match="on_behalf_of"):
        a.verify(jws, now=1_001)
    claims = {**_mint(monkeypatch)[1]}
    claims["operation"] = {**claims["operation"], "operands_hash": "sha256:00"}  # does not match operands
    with pytest.raises(a.AuthorityError, match="operands_hash"):
        a.verify(a._sign(claims, typ=a.OBJECT_TYP), now=1_001)


def test_record_path(monkeypatch):
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    rec = a.sign_record({"task": "t", "iat": 1000})
    assert a.verify(rec, now=1_001, typ=a.RECORD_TYP)["task"] == "t"
    with pytest.raises(a.AuthorityError, match="typ"):
        a.verify(rec, now=1_001)


def test_claim_authority_id_is_org_scoped_and_once(repos):
    import asyncio

    from core.repositories import a2a_repo

    assert asyncio.run(a2a_repo.claim_authority_id("org_1", "id-1", 1_000)) is True
    assert asyncio.run(a2a_repo.claim_authority_id("org_1", "id-1", 1_000)) is False
    assert asyncio.run(a2a_repo.claim_authority_id("org_2", "id-1", 1_000)) is True


def test_tampered_operands_break_signature(monkeypatch):
    import base64
    import json

    jws, claims = _mint(monkeypatch)
    assert claims["operation"]["operands"] == {"text": "hi"}
    head, body, sig = jws.split(".")
    payload = json.loads(a._unb64(body))
    payload["operation"]["operands"] = {"text": "rm -rf"}
    forged = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    with pytest.raises(a.AuthorityError, match="signature"):
        a.verify(f"{head}.{forged}.{sig}", now=1_001)


def test_unencodable_operands_fail_as_operands_in_mint_and_verify(monkeypatch):
    lone = a.AuthorityRequest(
        operation=a.Operation(skill="message", resource="agent:friday", operands={"text": "\ud800"}), intent="x"
    )
    with pytest.raises(a.AuthorityError, match="operands"):
        _mint(monkeypatch, request=lone)
    _, claims = _mint(monkeypatch)
    claims["operation"]["operands"] = {"text": "\ud800"}  # _sign escapes it; json.loads brings it back
    jws = a.network_proof._sign(claims, typ=a.OBJECT_TYP)
    with pytest.raises(a.AuthorityError) as e:
        a.verify(jws, now=1_001)
    assert e.value.check == "operands"
