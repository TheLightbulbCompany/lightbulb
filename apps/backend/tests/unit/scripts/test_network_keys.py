"""The issuer key ceremony: seeds go to Secrets Manager and nowhere else."""

import base64
import re

import boto3
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from moto import mock_aws

from scripts import network_keys as script


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-east-1")


def test_keys_script_never_prints_or_stores_a_seed_outside_secrets_manager(sm, tmp_path, capsys):
    assert script.main(sm) == 0
    out = capsys.readouterr()
    printed = re.findall(r'\("(\w+)", \[([\d, ]+)\]\), // (\S+)', out.out)
    assert [kid for kid, *_ in printed] == ["p1", "p2", "dev"]
    for (env, kid), (_, numbers, b64) in zip(script.KEYS, printed):
        seed_b64 = sm.get_secret_value(SecretId=f"lightbulb/{env}/network/issuer_key_{kid}")["SecretString"]
        seed = base64.b64decode(seed_b64)
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
        assert bytes(int(n) for n in numbers.split(", ")) == public and base64.b64decode(b64) == public
        assert f'"{kid}": bytes.fromhex("{public.hex()}")' in out.out  # the backend's table
        for spelling in (seed_b64, seed.hex(), ", ".join(map(str, seed)), base64.urlsafe_b64encode(seed).decode()):
            assert spelling not in out.out + out.err
    assert list(tmp_path.iterdir()) == []  # nothing written to disk


def test_keys_script_skips_an_existing_key_and_creates_the_missing_ones(sm, capsys):
    sm.create_secret(Name="lightbulb/dev/network/issuer_key_dev", SecretString="kept")
    assert script.main(sm) == 0
    out = capsys.readouterr()
    assert sm.get_secret_value(SecretId="lightbulb/dev/network/issuer_key_dev")["SecretString"] == "kept"
    assert len(sm.list_secrets()["SecretList"]) == 3
    assert "issuer_key_dev already exists; skipped" in out.err
    assert re.findall(r'\("(\w+)", \[', out.out) == ["p1", "p2"]
    assert script.main(sm) == 0 and "ISSUER_KEYS" not in capsys.readouterr().out  # a rerun creates nothing
