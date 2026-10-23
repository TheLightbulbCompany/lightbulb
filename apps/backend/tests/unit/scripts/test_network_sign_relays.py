"""scripts.network_sign_relays: keys go to Secrets Manager only, and the output is the
minisign shape the engine's src/relay.rs verifies (prehashed "ED", trusted comment)."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts import network_sign_relays as nsr


class FakeSecrets:
    class exceptions:  # noqa: N801 -- mirrors boto3's client.exceptions
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self):
        self.store: dict[str, str] = {}

    def get_secret_value(self, SecretId):  # noqa: N803
        if SecretId not in self.store:
            raise self.exceptions.ResourceNotFoundException(SecretId)
        return {"SecretString": self.store[SecretId]}

    def create_secret(self, Name, SecretString, Description):  # noqa: N803
        assert Name not in self.store
        self.store[Name] = SecretString


def verify(public: str, body: bytes, minisig: str) -> None:
    """What minisign-verify checks (allow_legacy = false): raises on any mismatch."""
    pk = base64.b64decode(public)
    lines = minisig.splitlines()
    assert pk[:2] == b"Ed" and lines[0].startswith("untrusted comment: ")
    sig = base64.b64decode(lines[1])
    assert sig[:2] == b"ED" and sig[2:10] == pk[2:10]
    key = Ed25519PublicKey.from_public_bytes(pk[10:])
    key.verify(sig[10:], hashlib.blake2b(body, digest_size=64).digest())
    trusted = lines[2].removeprefix("trusted comment: ")
    key.verify(base64.b64decode(lines[3]), sig[10:] + trusted.encode())


def test_generate_writes_both_keys_and_prints_only_public(capsys):
    sm = FakeSecrets()
    assert nsr.main(["--generate"], sm=sm) == 0
    assert set(sm.store) == {
        "lightbulb/prod/network/relays_signing_key_1",
        "lightbulb/prod/network/relays_signing_key_2",
    }
    out = capsys.readouterr().out
    for secret in sm.store.values():
        assert secret not in out
    publics = [line.rsplit(" ", 1)[1] for line in out.splitlines()]
    assert len(publics) == 2 and all(len(base64.b64decode(p)) == 42 for p in publics)


def test_generate_refuses_to_replace_a_key():
    sm = FakeSecrets()
    sm.store[nsr.SECRET_NAMES[1]] = "keep"
    with pytest.raises(SystemExit):
        nsr.generate(sm)
    assert sm.store == {nsr.SECRET_NAMES[1]: "keep"}


@pytest.mark.parametrize(
    "env,url", [("prod", "https://relay.thelightbulb.company"), ("dev", "https://relay-dev.thelightbulb.company")]
)
def test_sign_writes_a_list_that_verifies_with_key_1(tmp_path: Path, env, url):
    sm = FakeSecrets()
    publics = nsr.generate(sm)
    out = nsr.sign_env(sm, env, out_dir=tmp_path, now=1_000)
    body = (out / "relays.json").read_bytes()
    assert json.loads(body) == {"v": 1, "relays": [url], "expires_at": 1_000 + 90 * 86_400}
    minisig = (out / "relays.json.minisig").read_text()
    verify(publics[0], body, minisig)
    with pytest.raises((InvalidSignature, AssertionError)):
        verify(publics[1], body, minisig)
    with pytest.raises(InvalidSignature):
        verify(publics[0], body + b" ", minisig)


def test_committed_lists_are_the_signed_shape():
    for env, url in nsr.RELAYS.items():
        body = json.loads((nsr.OUT_DIR / env / "relays.json").read_text())
        assert body["v"] == 1 and body["relays"] == [url] and isinstance(body["expires_at"], int)
